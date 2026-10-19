"""What normalising a design's artwork on upload does to the documents drawn on it.

    python -m scripts.bench.doc_art [--runs 15]

Each artwork is drawn under a ticket, a certificate and an ID card (the
`classic` starters, the artwork as the canvas's template, a logo and an
icon), first as it was uploaded -- what the template columns stored, and
the designer too for anything up to 2400 px wide -- then as
app/core/storage/doc_art.py stores it now. Per artwork: the file, the time
`normalise` took, and per document the warm p50 render and the PDF's size.

The artworks are made here, from a fixed seed, so a run is repeatable:

- `smooth-jpeg`, `noise-jpeg`: 2000 x 1100 JPEGs of about the two sizes
  docs/stage0/AUDIT.md (2.3) measured, 54 KB and 1.36 MB: a smooth one, and
  noise (the worst case: nothing in it compresses, so it stays as it came).
- `photo-jpeg`: 2000 x 1100 at JPEG quality 98, already at its size: the
  same sort of file as the 1.36 MB one, with a photo's detail in it.
- `poster-png`: an A4 portrait export at 300 dpi (2480 x 3508) as a PNG,
  the way Figma or Canva hands a designer's poster over: gradients, shapes,
  a grain.
- `phone-photo`: 4032 x 3024 at JPEG quality 92 with EXIF orientation 6,
  as a phone takes it: photo-like texture at every scale.
- `logo-alpha`: 2000 x 800 PNG with transparency and soft edges.

Pure renders: no database, no bucket, no wkhtmltopdf (HTML designs).
"""
from __future__ import annotations

import argparse
import io
import statistics
import time

from scripts.bench import _env  # noqa: F401  (settings before app)

VALUES = {
    "attendee_name": "Ananya Iyer", "first_name": "Ananya", "last_name": "Iyer", "event_name": "Builders Day",
    "event_date": "16 Sept 2026", "event_time": "09:00", "venue": "KTPO", "org_name": "Yukthi", "ticket_type": "VIP",
    "ticket_code": "MOA-7K3QF-X9PWM", "certificate_no": "CERT-7K3QF-X9PWM", "issued_date": "18 Sept 2026", "role": "Attendee",
}
COLORS = {"primary": "#1db9a0", "secondary": "#aaaaaa", "ticketType": "#ff5a1f"}


def _texture(w: int, h: int, rng, octaves: int = 7, channels: int = 3):
    """Noise at every scale, the bigger the stronger: about how a photo's detail falls off."""
    import numpy as np
    from PIL import Image

    out = np.zeros((h, w, channels), dtype=np.float32)
    for o in range(octaves):
        cells = 2 ** (o + 2)
        gw, gh = max(2, w * cells // max(w, h)), max(2, h * cells // max(w, h))
        for c in range(channels):
            grid = Image.fromarray((rng.random((gh, gw)) * 255).astype(np.uint8), "L").resize((w, h), Image.Resampling.BICUBIC)
            out[:, :, c] += (np.asarray(grid, dtype=np.float32) - 128) / (1.6 ** o)
    out -= out.min()
    out /= max(1e-6, out.max())
    return out


def _encode(image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    image.save(buf, fmt, **kw)
    return buf.getvalue()


def artworks() -> dict[str, bytes]:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    rng = np.random.default_rng(20261019)
    out: dict[str, bytes] = {}

    # A smooth 2000 x 1100: two gradients and a few soft shapes.
    w, h = 2000, 1100
    x = np.linspace(0, 1, w)[None, :, None]
    y = np.linspace(0, 1, h)[:, None, None]
    base = np.concatenate([30 + 180 * x + 0 * y, 60 + 120 * y + 0 * x, 140 + 80 * (1 - x) + 0 * y], axis=2)
    img = Image.fromarray(base.clip(0, 255).astype(np.uint8), "RGB")
    d = ImageDraw.Draw(img)
    d.ellipse((1300, 200, 1800, 700), fill=(250, 200, 80))
    d.rectangle((120, 700, 900, 980), fill=(20, 30, 60))
    out["smooth-jpeg"] = _encode(img.filter(ImageFilter.GaussianBlur(2)), "JPEG", quality=80)

    noise = (rng.random((h, w, 3)) * 255).astype(np.uint8)
    out["noise-jpeg"] = _encode(Image.fromarray(noise, "RGB"), "JPEG", quality=78)
    out["photo-jpeg"] = _encode(Image.fromarray((_texture(w, h, rng, octaves=8) * 255).astype(np.uint8), "RGB"), "JPEG", quality=98)

    # A poster: gradient, texture, panels, a grain. A4 at 300 dpi, PNG.
    w, h = 2480, 3508
    t = _texture(w, h, rng, octaves=5)
    y = np.linspace(0, 1, h)[:, None, None]
    poster = (np.concatenate([40 + 160 * y + 0 * t[:, :, :1], 20 + 60 * (1 - y) + 0 * t[:, :, :1], 90 + 120 * y + 0 * t[:, :, :1]], axis=2)
              + (t - 0.5) * 70 + rng.normal(0, 3, (h, w, 3)))
    img = Image.fromarray(poster.clip(0, 255).astype(np.uint8), "RGB")
    d = ImageDraw.Draw(img)
    d.rectangle((160, 2400, 2320, 3300), fill=(250, 246, 236))
    d.ellipse((900, 500, 1900, 1500), outline=(255, 255, 255), width=24)
    for i in range(12):
        d.rectangle((260, 2500 + i * 60, 260 + 1600 - i * 90, 2530 + i * 60), fill=(30, 30, 40))
    out["poster-png"] = _encode(img, "PNG")

    # A phone photo: texture at every scale, sideways in the file (EXIF 6).
    w, h = 4032, 3024
    photo = _texture(w, h, rng, octaves=8) * 255
    img = Image.fromarray(photo.clip(0, 255).astype(np.uint8), "RGB")
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x010F] = "BenchCam"
    out["phone-photo"] = _encode(img, "JPEG", quality=92, exif=exif.tobytes())

    # A logo with transparency and soft (anti-aliased) edges.
    w, h = 2000, 800
    logo = Image.new("RGBA", (w * 2, h * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(logo)
    d.ellipse((100, 100, 1500, 1500), fill=(29, 185, 160, 255))
    d.rounded_rectangle((1700, 400, 3800, 1200), radius=200, fill=(20, 22, 20, 255))
    d.ellipse((500, 500, 1100, 1100), fill=(255, 255, 255, 0))
    out["logo-alpha"] = _encode(logo.resize((w, h), Image.Resampling.LANCZOS), "PNG")
    return out


def _documents():
    from PIL import Image

    from app.core.layout.renderer import RenderInput, render_pdf
    from app.core.layout.starters import badge_starter, certificate_starter, ticket_starter

    def png(w, h, color):
        return _encode(Image.new("RGB", (w, h), color), "PNG")

    logo, icon = png(512, 512, (20, 120, 200)), png(800, 800, (200, 80, 40))
    starters = {"ticket": ticket_starter("classic"), "certificate": certificate_starter("classic"), "idcard": badge_starter("classic")}

    def drawer(s, art: bytes):
        def draw() -> bytes:
            inp = RenderInput(title="bench", canvas=s["canvas"], layers=s["layers"], page=s["page"], values=VALUES,
                              qr_value="MOA-7K3QF-X9PWM", colors=COLORS, org_logo=logo, event_icon=icon)
            inp.template = art
            return render_pdf(inp)
        return draw

    return starters, drawer


def _p50(fn, runs: int) -> tuple[float, bytes]:
    fn()  # warm: fonts registered, the image decoded once
    xs, pdf = [], b""
    for _ in range(runs):
        t0 = time.perf_counter()
        pdf = fn()
        xs.append((time.perf_counter() - t0) * 1000)
    return statistics.median(xs), pdf


def main(runs: int) -> None:
    from PIL import Image

    from app.core.storage import doc_art

    starters, drawer = _documents()
    print(f"APP_DOC_ART_MAX_PX={doc_art.max_side()}, JPEG quality {doc_art.JPEG_QUALITY}; warm p50 of {runs} renders")
    print(f"{'artwork':<12} {'as uploaded':>24} {'stored now':>24} {'took':>7}  " + "  ".join(f"{k + ' (before -> after)':>40}" for k in starters))
    for name, raw in artworks().items():
        src = Image.open(io.BytesIO(raw))
        t0 = time.perf_counter()
        art = doc_art.normalise(raw, max_bytes=0)
        took = (time.perf_counter() - t0) * 1000
        before = f"{src.format} {src.width}x{src.height} {len(raw) / 1024:,.0f} KB"
        after = f"{art.format} {art.width}x{art.height} {len(art.data) / 1024:,.0f} KB"
        cells = []
        for s in starters.values():
            t_before, pdf_before = _p50(drawer(s, raw), runs)
            t_after, pdf_after = _p50(drawer(s, art.data), runs)
            cells.append(f"{t_before:6.1f} -> {t_after:5.1f} ms, {len(pdf_before) / 1024:6,.0f} -> {len(pdf_after) / 1024:4,.0f} KB")
        print(f"{name:<12} {before:>24} {after:>24} {took:5.0f}ms  " + "  ".join(f"{c:>40}" for c in cells), flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=int, default=15)
    main(ap.parse_args().runs)
