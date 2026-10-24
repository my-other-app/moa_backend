"""The demo's pictures, drawn from a club's brand: its logo, cover, event
posters, merch product shots, team and speaker portraits, partner marks,
and the artwork its ticket and certificate designs carry.

Everything is drawn with Pillow from the fonts the documents already use
(assets/fonts), so the seed needs nothing it does not have. Each function
takes the brand (four colours, a display font and a motif) and answers PNG
bytes at the size the app shows the picture at:

    logo            1024 x 1024   square, shown in circles and rounded tiles
    cover           1800 x 600    3:1, the club page's banner
    poster          1080 x 1350   4:5, how event posters are cut everywhere
    product         1200 x 1200   a store item on its own
    avatar           512 x 512    a team member or a speaker
    partner_mark     800 x 400    a year-round partner's wordmark

Shapes are drawn at twice the size and scaled down, so their edges are
smooth. Every choice that could vary is seeded from the club's slug, so a
club's pictures come out the same on every run.
"""

from __future__ import annotations

import io
import math
import random
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

FONTS = Path(__file__).resolve().parents[2] / "assets" / "fonts"

#: The display fonts a brand may name, and the file each is drawn with.
DISPLAY_FONTS = {
    "bebas": "bebas-400.ttf",
    "dm-serif": "dm-serif-400.ttf",
    "inter": "inter-700.ttf",
}

MOTIFS = ("waves", "grid", "circles", "stripes", "blobs", "triangles", "dots")

SS = 2  # drawn at twice the size, then scaled down: smooth edges


# ─── Colour ───────────────────────────────────────────────────────────────


def rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def mix(a, b, t: float) -> tuple[int, int, int]:
    """`a` moved `t` of the way to `b`."""
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def luminance(c) -> float:
    def channel(v):
        v = v / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in c[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def on(background, *choices):
    """Whichever of `choices` reads best on `background`."""
    return max(choices, key=lambda c: contrast(background, c))


def with_alpha(c, a: int) -> tuple[int, int, int, int]:
    return (*c[:3], a)


@dataclass(frozen=True)
class Brand:
    """A club's look, as the content files give it."""

    primary: str
    accent: str
    ink: str
    paper: str
    display_font: str = "inter"
    motif: str = "dots"

    @classmethod
    def of(cls, brand) -> "Brand":
        if isinstance(brand, Brand):
            return brand
        data = brand if isinstance(brand, dict) else brand.model_dump()
        return cls(**{k: data[k] for k in ("primary", "accent", "ink", "paper", "display_font", "motif")})

    @property
    def p(self):
        return rgb(self.primary)

    @property
    def a(self):
        return rgb(self.accent)

    @property
    def i(self):
        return rgb(self.ink)

    @property
    def w(self):
        return rgb(self.paper)


# ─── Fonts and text ───────────────────────────────────────────────────────


@lru_cache(maxsize=256)
def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    """A font by its short name ('bebas', 'dm-serif', 'inter', 'inter-500',
    'mono', 'mono-700', 'sans-600') at `size` pixels."""
    files = {
        "bebas": "bebas-400.ttf",
        "dm-serif": "dm-serif-400.ttf",
        "inter": "inter-700.ttf",
        "inter-400": "inter-400.ttf",
        "inter-500": "inter-500.ttf",
        "inter-600": "inter-600.ttf",
        "inter-700": "inter-700.ttf",
        "mono": "roboto-mono-400.ttf",
        "mono-700": "roboto-mono-700.ttf",
        "sans-400": "google-sans-400.ttf",
        "sans-500": "google-sans-500.ttf",
        "sans-600": "google-sans-600.ttf",
        "sans-700": "google-sans-700.ttf",
    }
    return ImageFont.truetype(str(FONTS / files[name]), size=max(1, int(size)))


def display(brand: Brand, size: int) -> ImageFont.FreeTypeFont:
    return font(brand.display_font if brand.display_font in DISPLAY_FONTS else "inter", size)


def caps(brand: Brand, text: str) -> str:
    """Bebas only has capitals; the others set headlines as written."""
    return text.upper() if brand.display_font == "bebas" else text


def wrap(draw, text: str, f, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=f) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def fit_lines(draw, text: str, make_font, width: int, max_lines: int, start: int, floor: int):
    """The largest size from `start` down at which `text` wraps into
    `max_lines` lines of `width`; answers (font, lines)."""
    size = start
    while True:
        f = make_font(size)
        lines = wrap(draw, text, f, width)
        if (len(lines) <= max_lines and all(draw.textlength(ln, font=f) <= width for ln in lines)) or size <= floor:
            return f, lines
        size = int(size * 0.92)


def tracked(draw, xy, text: str, f, fill, tracking: float = 0.0):
    """Text with letter spacing (`tracking` in ems); answers the width drawn."""
    x, y = xy
    em = f.size
    for ch in text:
        draw.text((x, y), ch, font=f, fill=fill)
        x += draw.textlength(ch, font=f) + tracking * em
    return x - xy[0] - (tracking * em if text else 0)


def initials(name: str, most: int = 2) -> str:
    skip = {"the", "of", "and", "&", "for", "a", "an", "club", "society", "collective"}
    words = [w for w in name.replace("-", " ").split() if w]
    picked = [w for w in words if w.lower() not in skip] or words
    letters = "".join(w[0] for w in picked[:most]).upper()
    return letters or name[:1].upper()


# ─── Canvas helpers ───────────────────────────────────────────────────────


def canvas(w: int, h: int, colour) -> Image.Image:
    return Image.new("RGBA", (w * SS, h * SS), with_alpha(colour, 255))


def finish(img: Image.Image, size: tuple[int, int], fmt: str = "PNG") -> bytes:
    if img.size != size:
        img = img.resize(size, Image.Resampling.LANCZOS)
    out = io.BytesIO()
    if fmt == "JPEG":
        img.convert("RGB").save(out, format="JPEG", quality=90, optimize=True, progressive=True)
    else:
        img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def linear_gradient(size, start, end, angle: float = 135) -> Image.Image:
    """A smooth two-colour gradient across `size` at `angle` degrees."""
    w, h = size
    small = 256
    rad = math.radians(angle)
    dx, dy = math.cos(rad), math.sin(rad)
    grad = Image.new("RGBA", (small, small))
    px = grad.load()
    for y in range(small):
        for x in range(small):
            t = ((x / (small - 1) - 0.5) * dx + (y / (small - 1) - 0.5) * dy) / (abs(dx) / 2 + abs(dy) / 2) / 2 + 0.5
            t = min(1.0, max(0.0, t))
            px[x, y] = with_alpha(mix(start, end, t), 255)
    return grad.resize((w, h), Image.Resampling.BICUBIC)


def radial_glow(size, centre, radius, colour, strength: int = 140) -> Image.Image:
    """A soft round light at `centre`, fading out by `radius`."""
    w, h = size
    layer = Image.new("RGBA", (w, h), with_alpha(colour, 0))
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    cx, cy = centre
    d.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=strength)
    mask = mask.filter(ImageFilter.GaussianBlur(radius * 0.45))
    layer.putalpha(mask)
    solid = Image.new("RGBA", (w, h), with_alpha(colour, 255))
    solid.putalpha(mask)
    return solid


def paste_layer(base: Image.Image, layer: Image.Image, opacity: float = 1.0, xy=(0, 0)) -> None:
    if opacity < 1.0:
        alpha = layer.getchannel("A").point(lambda v: int(v * opacity))
        layer = layer.copy()
        layer.putalpha(alpha)
    base.alpha_composite(layer, dest=xy)


def shadow(size, box, radius, blur, alpha=90, offset=(0, 0), colour=(0, 0, 0)) -> Image.Image:
    layer = Image.new("RGBA", size, with_alpha(colour, 0))
    m = Image.new("L", size, 0)
    x0, y0, x1, y1 = box
    ImageDraw.Draw(m).rounded_rectangle((x0 + offset[0], y0 + offset[1], x1 + offset[0], y1 + offset[1]), radius=radius, fill=alpha)
    m = m.filter(ImageFilter.GaussianBlur(blur))
    solid = Image.new("RGBA", size, with_alpha(colour, 255))
    solid.putalpha(m)
    return solid


# ─── Motifs ───────────────────────────────────────────────────────────────


def motif_layer(size, motif: str, colour, seed: str, scale: float = 1.0, weight: float = 1.0) -> Image.Image:
    """The motif drawn in `colour` over a transparent layer of `size`.

    `scale` sizes the pattern's units against the layer's shorter side;
    `weight` thickens its lines. Opacity is left to the caller."""
    w, h = size
    rnd = random.Random(f"{seed}:{motif}")
    layer = Image.new("RGBA", (w, h), with_alpha(colour, 0))
    d = ImageDraw.Draw(layer)
    unit = max(8, int(min(w, h) * 0.09 * scale))
    ink = with_alpha(colour, 255)
    line = max(2, int(unit * 0.09 * weight))

    if motif == "waves":
        gap = int(unit * 0.7)
        amp = unit * 0.32
        period = unit * 3.2
        phase0 = rnd.random() * math.tau
        for row, y0 in enumerate(range(-gap, h + gap * 2, gap)):
            phase = phase0 + row * 0.55
            pts = [(x, y0 + amp * math.sin(x / period * math.tau + phase)) for x in range(-20, w + 21, max(4, w // 240))]
            d.line(pts, fill=ink, width=line, joint="curve")

    elif motif == "grid":
        step = unit
        for x in range(0, w + step, step):
            d.line([(x, 0), (x, h)], fill=ink, width=max(1, line // 2))
        for y in range(0, h + step, step):
            d.line([(0, y), (w, y)], fill=ink, width=max(1, line // 2))
        cols, rows = w // step + 1, h // step + 1
        for _ in range(int(cols * rows * 0.07)):
            cx, cy = rnd.randrange(cols), rnd.randrange(rows)
            pad = int(step * 0.18)
            d.rectangle((cx * step + pad, cy * step + pad, (cx + 1) * step - pad, (cy + 1) * step - pad), fill=ink)
        for _ in range(int(cols * rows * 0.05)):
            cx, cy = rnd.randrange(cols), rnd.randrange(rows)
            r = int(step * 0.12)
            d.ellipse((cx * step - r, cy * step - r, cx * step + r, cy * step + r), fill=ink)

    elif motif == "circles":
        centres = [(rnd.uniform(0.15, 0.85) * w, rnd.uniform(0.1, 0.9) * h) for _ in range(2)]
        reach = int(math.hypot(w, h))
        for i, (cx, cy) in enumerate(centres):
            step = int(unit * (0.55 if i == 0 else 0.8))
            for r in range(step, reach, step):
                d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=ink, width=line)
        for _ in range(6):
            r = rnd.uniform(0.2, 0.6) * unit
            cx, cy = rnd.uniform(0, w), rnd.uniform(0, h)
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=ink)

    elif motif == "stripes":
        band = int(unit * 0.42)
        gap = int(unit * 0.42)
        span = w + h
        for k, x in enumerate(range(-h, w + h, band + gap)):
            thick = band if k % 5 != 0 else int(band * 1.9)
            d.polygon([(x, 0), (x + thick, 0), (x + thick - h, h), (x - h, h)], fill=ink)
        del span

    elif motif == "blobs":
        for _ in range(7):
            cx, cy = rnd.uniform(-0.05, 1.05) * w, rnd.uniform(-0.1, 1.1) * h
            r0 = rnd.uniform(1.2, 3.2) * unit
            harmonics = [(k, rnd.uniform(0.04, 0.16), rnd.uniform(0, math.tau)) for k in (2, 3, 5)]
            pts = []
            for s in range(120):
                t = s / 120 * math.tau
                r = r0 * (1 + sum(a * math.sin(k * t + ph) for k, a, ph in harmonics))
                pts.append((cx + r * math.cos(t), cy + r * math.sin(t)))
            d.polygon(pts, fill=with_alpha(colour, rnd.choice((150, 200, 255))))

    elif motif == "triangles":
        side = int(unit * 1.25)
        tri_h = side * math.sqrt(3) / 2
        rows = int(h / tri_h) + 2
        cols = int(w / side) + 3
        for r in range(-1, rows):
            for c in range(-1, cols):
                x = c * side + (side / 2 if r % 2 else 0)
                y = r * tri_h
                up = [(x, y + tri_h), (x + side / 2, y), (x + side, y + tri_h)]
                down = [(x + side / 2, y), (x + side * 1.5, y), (x + side, y + tri_h)]
                for tri in (up, down):
                    roll = rnd.random()
                    if roll < 0.16:
                        d.polygon(tri, fill=ink)
                    elif roll < 0.3:
                        d.polygon(tri, fill=with_alpha(colour, 110))
                    else:
                        d.line(tri + [tri[0]], fill=with_alpha(colour, 120), width=max(1, line // 2))

    elif motif == "dots":
        step = int(unit * 0.55)
        angle = rnd.uniform(0.2, 0.9)
        for yi, y in enumerate(range(0, h + step, step)):
            for x in range(-step, w + step, step):
                xx = x + (step / 2 if yi % 2 else 0)
                t = (xx / w * math.cos(angle) + y / h * math.sin(angle)) / (math.cos(angle) + math.sin(angle))
                r = step * 0.42 * (0.15 + 0.85 * (1 - min(1, max(0, t))))
                if r > 0.8:
                    d.ellipse((xx - r, y - r, xx + r, y + r), fill=ink)
    else:
        raise ValueError(f"unknown motif {motif!r}")
    return layer


# ─── Logo ─────────────────────────────────────────────────────────────────


def logo(brand, name: str, slug: str, size: int = 1024) -> bytes:
    """A monogram emblem: the club's initials in its display font on its
    primary colour, with the motif and an accent ring. Square and full-bleed,
    with everything that matters inside the middle circle, as the app cuts
    logos into circles."""
    b = Brand.of(brand)
    S = size * SS
    img = Image.new("RGBA", (S, S), with_alpha(b.p, 255))
    # A deeper corner, and the motif faintly across the whole tile.
    img.alpha_composite(radial_glow((S, S), (S * 0.15, S * 0.1), S * 0.9, mix(b.p, (255, 255, 255), 0.25), 150))
    img.alpha_composite(radial_glow((S, S), (S * 0.95, S * 1.0), S * 0.7, mix(b.p, (0, 0, 0), 0.35), 170))
    pattern = motif_layer((S, S), b.motif, b.a, slug, scale=0.9, weight=1.1)
    paste_layer(img, pattern, 0.16)

    d = ImageDraw.Draw(img)
    # The disc: paper or ink, whichever stands off the primary better.
    disc = on(b.p, b.w, b.i)
    r = int(S * 0.36)
    c = S // 2
    ring_w = int(S * 0.022)
    d.ellipse((c - r - ring_w * 2.4, c - r - ring_w * 2.4, c + r + ring_w * 2.4, c + r + ring_w * 2.4), outline=with_alpha(b.a, 255), width=ring_w)
    d.ellipse((c - r, c - r, c + r, c + r), fill=with_alpha(disc, 255))

    letters = initials(name)
    face = on(disc, b.p, b.i, b.a)
    if contrast(disc, b.p) >= 3:
        face = b.p
    target_w = r * (1.15 if len(letters) > 1 else 0.8)
    fsize = int(r * 1.1)
    f = display(b, fsize)
    while d.textlength(letters, font=f) > target_w and fsize > 20:
        fsize = int(fsize * 0.94)
        f = display(b, fsize)
    box = d.textbbox((0, 0), letters, font=f, anchor="ls")
    tw, th = box[2] - box[0], box[3] - box[1]
    d.text((c - tw / 2 - box[0], c + th / 2), letters, font=f, fill=with_alpha(face, 255), anchor="ls")
    # A small accent mark under the letters.
    bar_w, bar_h = int(r * 0.34), max(4, int(S * 0.012))
    y = c + th / 2 + r * 0.16
    d.rounded_rectangle((c - bar_w / 2, y, c + bar_w / 2, y + bar_h), radius=bar_h // 2, fill=with_alpha(b.a, 255))
    return finish(img, (size, size))


def mark(brand, name: str, slug: str, size: int) -> Image.Image:
    """The logo as an RGBA image cut to a circle, for drawing onto others."""
    tile = Image.open(io.BytesIO(logo(brand, name, slug, size=max(256, size)))).convert("RGBA")
    tile = tile.resize((size, size), Image.Resampling.LANCZOS)
    m = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(m).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    tile.putalpha(m.resize((size, size), Image.Resampling.LANCZOS))
    return tile


# ─── Cover ────────────────────────────────────────────────────────────────


def _disc_with_motif(size, centre, radius, fill, motif, motif_colour, seed, opacity=0.35, scale=1.0) -> Image.Image:
    """A filled disc with the motif drawn inside it: the focal shape of a
    cover or a poster."""
    w, h = size
    layer = Image.new("RGBA", (w, h), with_alpha(fill, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = centre
    d.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=with_alpha(fill, 255))
    inner = motif_layer((w, h), motif, motif_colour, seed, scale=scale, weight=1.1)
    clip = Image.new("L", (w, h), 0)
    ImageDraw.Draw(clip).ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=255)
    inner.putalpha(ImageChops.multiply(inner.getchannel("A"), clip).point(lambda v: int(v * opacity)))
    layer.alpha_composite(inner)
    return layer


def cover(brand, slug: str, size=(1800, 600)) -> bytes:
    """The club page's banner: a deep field of the primary colour, the motif
    in the accent gathering towards the right, and a large accent disc
    carrying the motif as the focal shape. No words: the page sets the name
    over it, on the left, where the field is quiet."""
    b = Brand.of(brand)
    W, H = size[0] * SS, size[1] * SS
    rnd = random.Random(slug + ":cover")
    deep = mix(b.p, (0, 0, 0), 0.38)
    img = linear_gradient((W, H), deep, mix(b.p, (255, 255, 255), 0.04), angle=15)
    img.alpha_composite(radial_glow((W, H), (W * 0.78, H * 0.35), H * 1.1, mix(b.p, (255, 255, 255), 0.3), 70))
    pattern = motif_layer((W, H), b.motif, b.a, slug, scale=1.15, weight=1.0)
    fade = linear_gradient((W, H), (0, 0, 0), (255, 255, 255), angle=0).convert("L")
    fade = fade.point(lambda v: int(max(0, v - 70) * 1.5))
    pattern.putalpha(ImageChops.multiply(pattern.getchannel("A"), fade).point(lambda v: int(min(255, v) * 0.42)))
    img.alpha_composite(pattern)
    # The focal disc, half off the right edge, in a tone of the accent with
    # the motif cut into it a shade deeper, and a small echo of it.
    r = H * rnd.uniform(0.62, 0.75)
    cx, cy = W - r * rnd.uniform(0.35, 0.55), H * rnd.uniform(0.35, 0.65)
    disc = mix(b.a, deep, 0.12)
    img.alpha_composite(_disc_with_motif((W, H), (cx, cy), r, disc, b.motif, mix(b.a, deep, 0.45), slug + ":disc", 0.85, 0.9))
    echo = _disc_with_motif((W, H), (cx - r * 1.3, cy + r * 0.5), r * 0.13, b.w if luminance(b.w) > 0.5 else (255, 255, 255), b.motif, deep, slug + ":echo", 0.0)
    paste_layer(img, echo, 0.85)
    paste_layer(img, motif_layer((W, H), b.motif, b.w, slug + ":fine", scale=0.5, weight=0.7), 0.05)
    return finish(img, size, "JPEG")


# ─── Poster ───────────────────────────────────────────────────────────────


def poster(
    brand,
    *,
    club_name: str,
    slug: str,
    headline: str,
    sub: str,
    when: datetime,
    place: str,
    category: str,
    price: str,
    size=(1080, 1350),
) -> bytes:
    """An event poster, 4:5: the club's mark and name at the head, a large
    accent disc carrying the motif as the artwork, the headline in the
    display font beneath it, the sub line, and a paper panel at the foot
    with the date, the time, the place and the price."""
    b = Brand.of(brand)
    W, H = size[0] * SS, size[1] * SS
    pad = int(W * 0.075)
    seed = f"{slug}:{headline}"
    rnd = random.Random(seed)

    # Some posters on the deep ink, some on the primary: a club's run of
    # events reads as a set without every one being the same.
    dark = rnd.random() < 0.5
    ground = mix(b.i, b.p, 0.28) if dark else b.p
    img = linear_gradient((W, H), mix(ground, (0, 0, 0), 0.22), mix(ground, (255, 255, 255), 0.05), angle=250)
    img.alpha_composite(radial_glow((W, H), (W * 0.75, H * 0.25), W * 0.8, mix(ground, (255, 255, 255), 0.35), 60))

    d = ImageDraw.Draw(img)
    text = on(ground, b.w, (255, 255, 255), b.i)
    soft = mix(text, ground, 0.28)

    # The type, measured first and set from the foot up, so the artwork can
    # take whatever room is left above it.
    head = caps(b, headline)
    width = W - pad * 2
    start = int(W * (0.15 if b.display_font == "bebas" else 0.105))
    hf, lines = fit_lines(d, head, lambda s: display(b, s), width, 3, start, int(W * 0.05))
    line_h = int(hf.size * (0.96 if b.display_font == "bebas" else 1.1))
    sf = font("sans-500", int(W * 0.034))
    sub_lines = wrap(d, sub, sf, width)[:2]
    foot_h = int(H * 0.19)
    fy0 = H - pad - foot_h
    sub_h = int(sf.size * 1.35) * len(sub_lines)
    head_y = fy0 - int(H * 0.04) - sub_h - int(H * 0.05) - line_h * len(lines)
    cat_y = head_y - int(H * 0.045)

    # The artwork: the motif faintly over the top, and the disc between the
    # club's mark and the category line, bleeding off the right edge.
    m = mark(b, club_name, slug, int(W * 0.085))
    top = motif_layer((W, max(10, cat_y)), b.motif, b.a, seed, scale=1.2, weight=1.0)
    fade = Image.new("L", top.size, 0)
    fd = ImageDraw.Draw(fade)
    for yy in range(top.size[1]):
        t = yy / top.size[1]
        fd.line([(0, yy), (top.size[0], yy)], fill=int(255 * max(0.0, 1 - t / 0.9)))
    top.putalpha(ImageChops.multiply(top.getchannel("A"), fade).point(lambda v: int(v * 0.26)))
    img.alpha_composite(top)
    # The disc sits in the top right, from the edge down to just above the
    # category line, clear of the club's name on the left.
    name_f = font("sans-600", int(W * 0.03))
    head_right = pad + m.size[0] + int(W * 0.022) + d.textlength(club_name, font=name_f) + int(W * 0.04)
    band1 = cat_y - int(H * 0.04)
    r = min(W * 0.42, band1 / 2)
    cx = W - r * 0.72
    if cx - r < head_right and band1 - 2 * r < pad + m.size[1]:
        # A long name: the disc moves down and shrinks to stay off it.
        r = min(r, (band1 - pad - m.size[1] - int(H * 0.02)) / 2)
        cx = W - r * 0.72
    cy = band1 - r
    disc = mix(b.a, ground, 0.08)
    img.alpha_composite(_disc_with_motif((W, H), (cx, cy), r, disc, b.motif, mix(b.a, ground, 0.42), seed + ":disc", 0.9, 0.7))
    r2 = r * 0.12
    dot_x, dot_y = cx - r - r2 * 1.6, band1 - r2 * 1.4
    if dot_x - r2 > max(pad, head_right if dot_y - r2 < pad + m.size[1] else pad):
        img.alpha_composite(_disc_with_motif((W, H), (dot_x, dot_y), r2, b.w if luminance(b.w) > 0.5 else (255, 255, 255), b.motif, ground, seed + ":dot", 0.0))
    d = ImageDraw.Draw(img)

    # Head: the club's mark and name.
    img.alpha_composite(m, dest=(pad, pad))
    d.text((pad + m.size[0] + int(W * 0.022), pad + m.size[1] / 2), club_name, font=name_f, fill=with_alpha(text, 255), anchor="lm")

    # The category, the headline, the rule and the sub line.
    cat_f = font("mono-700", int(W * 0.024))
    tracked(d, (pad, cat_y), category.upper(), cat_f, with_alpha(b.a if contrast(ground, b.a) > 2.2 else text, 255), 0.18)
    y = head_y
    for ln in lines:
        d.text((pad, y), ln, font=hf, fill=with_alpha(text, 255))
        y += line_h
    y += int(H * 0.018)
    d.rectangle((pad, y, pad + int(W * 0.12), y + max(4, SS * 4)), fill=with_alpha(b.a, 255))
    y += int(H * 0.032)
    for ln in sub_lines:
        d.text((pad, y), ln, font=sf, fill=with_alpha(soft, 255))
        y += int(sf.size * 1.35)

    # Foot: a paper panel with the date block, the day and time, the place, the price.
    panel = b.w if luminance(b.w) > 0.75 else (255, 255, 255)
    d.rounded_rectangle((pad, fy0, W - pad, H - pad), radius=int(W * 0.03), fill=with_alpha(panel, 255))
    pink = on(panel, b.i, (17, 17, 17))
    dw = int(foot_h * 0.8)
    dx0, dy0 = pad + int(foot_h * 0.1), fy0 + int(foot_h * 0.1)
    d.rounded_rectangle((dx0, dy0, dx0 + dw, dy0 + dw), radius=int(W * 0.02), fill=with_alpha(b.p, 255))
    on_p = on(b.p, b.w, (255, 255, 255), b.i)
    mf = font("mono-700", int(dw * 0.16))
    d.text((dx0 + dw / 2, dy0 + dw * 0.23), when.strftime("%b").upper(), font=mf, fill=with_alpha(on_p, 255), anchor="mm")
    nf = font("sans-700", int(dw * 0.46))
    d.text((dx0 + dw / 2, dy0 + dw * 0.6), str(when.day), font=nf, fill=with_alpha(on_p, 255), anchor="mm")
    tx = dx0 + dw + int(W * 0.04)
    tf = font("sans-700", int(W * 0.038))
    stamp = f"{when.strftime('%A')} · {when.strftime('%I:%M %p').lstrip('0')}"
    d.text((tx, dy0 + dw * 0.06), stamp, font=tf, fill=with_alpha(pink, 255))
    pf = font("sans-500", int(W * 0.029))
    prf = font("sans-700", int(W * 0.036))  # Roboto Mono has no rupee sign
    price_w = d.textlength(price.upper(), font=prf)
    room = W - pad - int(W * 0.05) - price_w - int(W * 0.03) - tx
    py = dy0 + dw * 0.06 + tf.size * 1.45
    for ln in wrap(d, place, pf, room)[:2]:
        d.text((tx, py), ln, font=pf, fill=with_alpha(mix(pink, panel, 0.3), 255))
        py += pf.size * 1.3
    price_ink = b.p if contrast(panel, b.p) > 2.5 else pink
    d.text((W - pad - int(W * 0.045), dy0 + dw / 2), price.upper(), font=prf, fill=with_alpha(price_ink, 255), anchor="rm")
    return finish(img, size, "JPEG")


# ─── Avatars and partner marks ────────────────────────────────────────────


def avatar(brand, name: str, size: int = 512, variant: int = 0) -> bytes:
    """A portrait stand-in: the person's initials on a gradient of the
    club's colours, turned a little differently for each person."""
    b = Brand.of(brand)
    S = size * SS
    pairs = [(b.p, b.a), (b.a, b.p), (mix(b.p, b.i, 0.45), b.p), (b.p, mix(b.a, b.w, 0.3))]
    start, end = pairs[variant % len(pairs)]
    img = linear_gradient((S, S), start, end, angle=120 + (variant * 37) % 90)
    img.alpha_composite(radial_glow((S, S), (S * 0.3, S * 0.25), S * 0.6, (255, 255, 255), 70))
    paste_layer(img, motif_layer((S, S), b.motif, (255, 255, 255), f"{name}:{variant}", scale=0.8), 0.08)
    d = ImageDraw.Draw(img)
    letters = initials(name)
    mid = mix(start, end, 0.5)
    f = font("sans-600", int(S * 0.3))
    d.text((S / 2, S / 2), letters, font=f, fill=with_alpha(on(mid, (255, 255, 255), b.i), 255), anchor="mm")
    return finish(img, (size, size), "JPEG")


def partner_mark(brand, name: str, size=(800, 400)) -> bytes:
    """A partner's wordmark: its name set plainly beside a small glyph, in
    a neutral ink on white, the way a partner's own logo would arrive."""
    b = Brand.of(brand)
    W, H = size[0] * SS, size[1] * SS
    img = Image.new("RGBA", (W, H), (255, 255, 255, 255))
    d = ImageDraw.Draw(img)
    rnd = random.Random(name)
    hue = rnd.choice([b.p, b.i, mix(b.p, b.i, 0.5), (34, 34, 34)])
    gx, gy, g = int(W * 0.1), H / 2, int(H * 0.26)
    shape = rnd.choice(("disc", "square", "diamond", "ring"))
    if shape == "disc":
        d.ellipse((gx, gy - g, gx + g * 2, gy + g), fill=with_alpha(hue, 255))
    elif shape == "square":
        d.rounded_rectangle((gx, gy - g, gx + g * 2, gy + g), radius=g // 3, fill=with_alpha(hue, 255))
    elif shape == "diamond":
        d.polygon([(gx + g, gy - g), (gx + g * 2, gy), (gx + g, gy + g), (gx, gy)], fill=with_alpha(hue, 255))
    else:
        d.ellipse((gx, gy - g, gx + g * 2, gy + g), outline=with_alpha(hue, 255), width=int(g * 0.38))
    words = name
    tx = gx + g * 2 + int(W * 0.04)
    room = W - tx - int(W * 0.06)
    face = rnd.choice(("sans-700", "inter-700", "dm-serif", "sans-600"))
    f, lines = fit_lines(d, words, lambda s: font(face, s), room, 2, int(H * 0.2), int(H * 0.08))
    lh = f.size * 1.1
    y = gy - lh * len(lines) / 2
    for ln in lines:
        d.text((tx, y), ln, font=f, fill=with_alpha(hue, 255))
        y += lh
    return finish(img, size)


# ─── Merch product shots ──────────────────────────────────────────────────


def _shade(c, amount: float):
    return mix(c, (0, 0, 0), amount) if amount > 0 else mix(c, (255, 255, 255), -amount)


def _print_mark(img, brand: Brand, name: str, slug: str, centre, width: int, garment):
    """The club's mark as printed on a product: the monogram in a colour
    that stands off the garment, with the club's name under it."""
    d = ImageDraw.Draw(img)
    ink = on(garment, brand.w, (255, 255, 255), brand.i, brand.a)
    if contrast(garment, brand.a) >= 2.2:
        ink2 = brand.a
    else:
        ink2 = ink
    letters = initials(name)
    cx, cy = centre
    f = display(brand, int(width * 0.55))
    while d.textlength(letters, font=f) > width * 0.8 and f.size > 10:
        f = display(brand, int(f.size * 0.92))
    d.text((cx, cy), letters, font=f, fill=with_alpha(ink, 255), anchor="mm")
    # The name under it, smaller until it fits; a long one loses whole words
    # from the end rather than letters.
    words = name.upper().split()
    size = max(10, int(width * 0.085))
    small = font("mono-700", size)
    while d.textlength(" ".join(words), font=small) > width * 1.1:
        if size > max(10, int(width * 0.065)):
            size = int(size * 0.92)
            small = font("mono-700", size)
        elif len(words) > 1:
            words = words[:-1]
        else:
            break
    d.text((cx, cy + f.size * 0.55), " ".join(words), font=small, fill=with_alpha(ink2, 255), anchor="mt")


def product(brand, *, kind: str, colour: str, name: str, slug: str, size: int = 1200) -> bytes:
    """A flat illustration of a store item -- tee, hoodie, cap, tote,
    sticker, mug, lanyard or notebook -- in the item's colour with the
    club's mark on it, on a soft studio background."""
    b = Brand.of(brand)
    S = size * SS
    c = rgb(colour)
    bg = mix(b.w, (255, 255, 255), 0.35) if luminance(b.w) > 0.6 else (244, 242, 238)
    if contrast(bg, c) < 1.25:
        bg = mix(bg, b.p, 0.18)
    img = linear_gradient((S, S), mix(bg, (255, 255, 255), 0.4), mix(bg, (0, 0, 0), 0.05), angle=90)
    paste_layer(img, motif_layer((S, S), b.motif, b.p, slug + ":studio", scale=1.2), 0.05)
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    dark, light = _shade(c, 0.18), _shade(c, -0.12)
    outline = with_alpha(_shade(c, 0.28), 255)
    lw = int(S * 0.004)

    def poly(pts, fill, edge=True):
        P = [(x * S, y * S) for x, y in pts]
        d.polygon(P, fill=with_alpha(fill, 255))
        if edge:
            d.line(P + [P[0]], fill=outline, width=lw, joint="curve")

    mark_at, mark_w = (0.5 * S, 0.47 * S), int(0.2 * S)

    if kind in ("tee", "hoodie"):
        body = [(0.3, 0.27), (0.39, 0.22), (0.61, 0.22), (0.7, 0.27), (0.88, 0.38), (0.81, 0.5), (0.72, 0.45),
                (0.72, 0.82), (0.28, 0.82), (0.28, 0.45), (0.19, 0.5), (0.12, 0.38)]
        if kind == "hoodie":
            body = [(0.3, 0.27), (0.38, 0.23), (0.62, 0.23), (0.7, 0.27), (0.82, 0.4), (0.86, 0.74), (0.77, 0.76),
                    (0.73, 0.5), (0.73, 0.84), (0.27, 0.84), (0.27, 0.5), (0.23, 0.76), (0.14, 0.74), (0.18, 0.4)]
        poly(body, c)
        if kind == "tee":
            d.chord((0.4 * S, 0.17 * S, 0.6 * S, 0.29 * S), 0, 180, fill=with_alpha(dark, 255), outline=outline, width=lw)
            d.line([(0.28 * S, 0.45 * S), (0.3 * S, 0.27 * S)], fill=outline, width=lw)
            d.line([(0.72 * S, 0.45 * S), (0.7 * S, 0.27 * S)], fill=outline, width=lw)
        else:
            # Hood, pocket, cuffs and strings.
            d.ellipse((0.36 * S, 0.13 * S, 0.64 * S, 0.34 * S), fill=with_alpha(dark, 255), outline=outline, width=lw)
            d.ellipse((0.41 * S, 0.19 * S, 0.59 * S, 0.33 * S), fill=with_alpha(_shade(c, 0.35), 255))
            poly([(0.36, 0.64), (0.64, 0.64), (0.68, 0.76), (0.32, 0.76)], dark)
            for x in (0.46, 0.54):
                d.line([(x * S, 0.32 * S), (x * S, 0.44 * S)], fill=with_alpha(light, 255), width=lw * 2)
            poly([(0.27, 0.8), (0.73, 0.8), (0.73, 0.84), (0.27, 0.84)], dark)
            mark_at, mark_w = (0.5 * S, 0.5 * S), int(0.17 * S)
        poly([(0.28, 0.79), (0.72, 0.79), (0.72, 0.82), (0.28, 0.82)], dark, edge=False)

    elif kind == "cap":
        # Front view: the crown in six panels, a band at its foot, the brim below.
        d.chord((0.25 * S, 0.3 * S, 0.75 * S, 0.9 * S), 180, 360, fill=with_alpha(c, 255), outline=outline, width=lw)
        for x0, x1 in ((0.38, 0.62), (0.31, 0.69)):
            d.arc((x0 * S, 0.3 * S, x1 * S, 0.9 * S), 180, 360, fill=with_alpha(dark, 255), width=lw)
        d.line([(0.5 * S, 0.3 * S), (0.5 * S, 0.36 * S)], fill=with_alpha(dark, 255), width=lw)
        d.ellipse((0.485 * S, 0.29 * S, 0.515 * S, 0.315 * S), fill=with_alpha(dark, 255))
        poly([(0.25, 0.585), (0.75, 0.585), (0.75, 0.6), (0.25, 0.6)], dark, edge=False)
        d.chord((0.23 * S, 0.5 * S, 0.77 * S, 0.74 * S), 0, 180, fill=with_alpha(_shade(c, 0.16), 255), outline=outline, width=lw)
        d.chord((0.29 * S, 0.56 * S, 0.71 * S, 0.68 * S), 0, 180, fill=with_alpha(_shade(c, 0.3), 255))
        # The front panels behind the print are re-filled so the seams don't cross it.
        d.chord((0.36 * S, 0.36 * S, 0.64 * S, 0.84 * S), 180, 360, fill=with_alpha(c, 255))
        mark_at, mark_w = (0.5 * S, 0.47 * S), int(0.15 * S)

    elif kind == "tote":
        d.arc((0.36 * S, 0.12 * S, 0.64 * S, 0.5 * S), 180, 360, fill=with_alpha(dark, 255), width=int(S * 0.025))
        poly([(0.26, 0.3), (0.74, 0.3), (0.77, 0.84), (0.23, 0.84)], c)
        poly([(0.26, 0.3), (0.74, 0.3), (0.745, 0.34), (0.255, 0.34)], dark, edge=False)
        mark_at, mark_w = (0.5 * S, 0.55 * S), int(0.22 * S)

    elif kind == "sticker":
        r = 0.27 * S
        cx, cy = 0.5 * S, 0.5 * S
        d.ellipse((cx - r - S * 0.02, cy - r - S * 0.02, cx + r + S * 0.02, cy + r + S * 0.02), fill=(255, 255, 255, 255), outline=(220, 220, 220, 255), width=lw)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=with_alpha(c, 255))
        ring = r * 0.86
        d.ellipse((cx - ring, cy - ring, cx + ring, cy + ring), outline=with_alpha(on(c, b.w, b.i), 120), width=lw * 2)
        mark_at, mark_w = (cx, cy - r * 0.08), int(r * 1.15)

    elif kind == "mug":
        d.rounded_rectangle((0.28 * S, 0.3 * S, 0.66 * S, 0.8 * S), radius=int(S * 0.03), fill=with_alpha(c, 255), outline=outline, width=lw)
        d.arc((0.58 * S, 0.4 * S, 0.8 * S, 0.66 * S), 270, 90, fill=with_alpha(c, 255), width=int(S * 0.04))
        d.arc((0.58 * S, 0.4 * S, 0.8 * S, 0.66 * S), 270, 90, fill=outline, width=lw)
        d.ellipse((0.28 * S, 0.27 * S, 0.66 * S, 0.33 * S), fill=with_alpha(dark, 255), outline=outline, width=lw)
        d.ellipse((0.3 * S, 0.28 * S, 0.64 * S, 0.32 * S), fill=with_alpha(_shade(c, 0.4), 255))
        d.rectangle((0.31 * S, 0.36 * S, 0.335 * S, 0.74 * S), fill=with_alpha(light, 255))
        mark_at, mark_w = (0.47 * S, 0.53 * S), int(0.2 * S)

    elif kind == "lanyard":
        strap = int(S * 0.06)
        d.line([(0.34 * S, 0.06 * S), (0.47 * S, 0.5 * S)], fill=with_alpha(c, 255), width=strap)
        d.line([(0.66 * S, 0.06 * S), (0.53 * S, 0.5 * S)], fill=with_alpha(dark, 255), width=strap)
        d.rounded_rectangle((0.46 * S, 0.48 * S, 0.54 * S, 0.55 * S), radius=int(S * 0.01), fill=(160, 160, 165, 255))
        d.rounded_rectangle((0.33 * S, 0.55 * S, 0.67 * S, 0.88 * S), radius=int(S * 0.025), fill=(255, 255, 255, 255), outline=(210, 210, 210, 255), width=lw)
        d.rectangle((0.33 * S, 0.55 * S, 0.67 * S, 0.62 * S), fill=with_alpha(c, 255))
        mark_at, mark_w = (0.5 * S, 0.72 * S), int(0.14 * S)
        _print_mark(layer, b, name, slug, mark_at, mark_w, (255, 255, 255))
        mark_at = None

    elif kind == "notebook":
        d.rounded_rectangle((0.3 * S, 0.17 * S, 0.72 * S, 0.83 * S), radius=int(S * 0.018), fill=(250, 248, 242, 255))
        d.rounded_rectangle((0.28 * S, 0.16 * S, 0.7 * S, 0.82 * S), radius=int(S * 0.018), fill=with_alpha(c, 255), outline=outline, width=lw)
        d.rectangle((0.6 * S, 0.16 * S, 0.625 * S, 0.82 * S), fill=with_alpha(dark, 255))
        d.rectangle((0.36 * S, 0.62 * S, 0.54 * S, 0.66 * S), fill=with_alpha(on(c, b.w, b.i), 200))
        mark_at, mark_w = (0.45 * S, 0.4 * S), int(0.2 * S)
    else:
        raise ValueError(f"unknown kind {kind!r}")

    # A floor shadow under the product's foot, a soft drop shadow, then the
    # product, then its print.
    box = layer.getchannel("A").getbbox() or (0, 0, S, S)
    x0, _, x1, y1 = box
    fm = Image.new("L", (S, S), 0)
    spread = (x1 - x0) * 0.42
    mid = (x0 + x1) / 2
    ImageDraw.Draw(fm).ellipse((mid - spread, y1 - S * 0.01, mid + spread, y1 + S * 0.035), fill=95)
    floor = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    floor.putalpha(fm.filter(ImageFilter.GaussianBlur(S * 0.018)))
    img.alpha_composite(floor)
    sh = layer.getchannel("A").filter(ImageFilter.GaussianBlur(S * 0.012))
    drop = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    drop.putalpha(sh.point(lambda v: int(v * 0.2)))
    img.alpha_composite(drop, dest=(int(S * 0.006), int(S * 0.012)))
    img.alpha_composite(layer)
    if mark_at is not None:
        _print_mark(img, b, name, slug, mark_at, mark_w, c)
    return finish(img, (size, size), "JPEG")


# ─── Ticket and certificate artwork ───────────────────────────────────────


def ticket_art(brand, slug: str, size=(1200, 540)) -> bytes:
    """The background a club's ticket design carries: the primary colour,
    the motif in the accent towards the right, a softer glow behind."""
    b = Brand.of(brand)
    W, H = size[0] * SS, size[1] * SS
    img = linear_gradient((W, H), mix(b.p, (0, 0, 0), 0.25), b.p, angle=160)
    img.alpha_composite(radial_glow((W, H), (W * 0.9, H * 0.1), H * 1.2, b.a, 110))
    pattern = motif_layer((W, H), b.motif, b.a, slug + ":ticket", scale=1.3, weight=1.2)
    fade = linear_gradient((W, H), (0, 0, 0), (255, 255, 255), angle=0).convert("L")
    pattern.putalpha(ImageChops.multiply(pattern.getchannel("A"), fade).point(lambda v: int(v * 0.5)))
    img.alpha_composite(pattern)
    return finish(img, size, "JPEG")


def certificate_art(brand, slug: str, size=(1754, 1240)) -> bytes:
    """The background a club's certificate design carries: paper, a deep
    band of the primary down the left with the motif in it, a fine accent
    frame, and a faint motif in the open paper."""
    b = Brand.of(brand)
    W, H = size[0] * SS, size[1] * SS
    paper = b.w if luminance(b.w) > 0.7 else (250, 248, 243)
    img = Image.new("RGBA", (W, H), with_alpha(paper, 255))
    paste_layer(img, motif_layer((W, H), b.motif, b.p, slug + ":paper", scale=0.8, weight=0.8), 0.045)
    band_w = int(W * 0.16)
    band = linear_gradient((band_w, H), mix(b.p, (0, 0, 0), 0.3), b.p, angle=90)
    band.alpha_composite(motif_layer((band_w, H), b.motif, b.a, slug + ":band", scale=0.35 * W / band_w * 0.5, weight=1.1).point(lambda v: v) if False else _faded(motif_layer((band_w, H), b.motif, b.a, slug + ":band", scale=2.2, weight=1.1), 0.55))
    img.alpha_composite(band)
    d = ImageDraw.Draw(img)
    d.rectangle((band_w, 0, band_w + int(W * 0.006), H), fill=with_alpha(b.a, 255))
    inset = int(W * 0.03)
    d.rectangle((band_w + inset, inset, W - inset, H - inset), outline=with_alpha(mix(b.a, paper, 0.25), 255), width=max(2, int(W * 0.0018)))
    d.rectangle((band_w + inset + int(W * 0.007), inset + int(W * 0.007), W - inset - int(W * 0.007), H - inset - int(W * 0.007)), outline=with_alpha(mix(b.p, paper, 0.55), 255), width=max(1, int(W * 0.0009)))
    return finish(img, size, "JPEG")


def _faded(layer: Image.Image, opacity: float) -> Image.Image:
    layer = layer.copy()
    layer.putalpha(layer.getchannel("A").point(lambda v: int(v * opacity)))
    return layer
