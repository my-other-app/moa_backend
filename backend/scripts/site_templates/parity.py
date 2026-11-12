"""Pixel parity and mobile checks for the club-site template packages.

    cd backend
    .venv_test/bin/python scripts/site_templates/parity.py              # every package
    .venv_test/bin/python scripts/site_templates/parity.py classic night  # some
    options: --src DIR (the designer's files), --out DIR (screenshots),
             --no-thumbs, --no-extras, --threshold 99.5

For each package: render it with its sample.json (render.py: plain Jinja2,
the platform's rules), screenshot the designer's file and ours at 1440 px,
full page, in headless Chromium, and compare pixel by pixel (identical RGBA)
-> <out>/<key>/diff-1440.png and ours-1440.png. Then our page at 390 and 768
px wide: no horizontal overflow (scrollWidth <= viewport, no element past
the edge) -> m390.png, m768.png. Extras (--no-extras to skip): the page with
real pictures, a club with nothing but a name, very long names, and the
sections in reverse order, each at 1440 and the phone/tablet widths. Then
the dashboard thumbnails thumb.png (600x400) and thumb-mobile.png (300x600)
into the package.

Screenshots are taken by shoot.cjs (Node + playwright-core; see its header
for the tools folder and the browser). Exit status 1 when a package is under
the threshold or overflows.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import render as R  # noqa: E402
import variants as V  # noqa: E402

HOME = Path.home()
DEFAULT_SRC = HOME / "development/projects/feature-register/website-templates-src/website-templates"
DEFAULT_OUT = HOME / "development/projects/feature-register/site-parity"
HERE = Path(__file__).resolve().parent


def source_for(key: str, src: Path) -> Path | None:
    hits = sorted(src.glob(f"*-{key}.html"))
    return hits[0] if hits else None


def make_assets(out: Path) -> list[str]:
    """A few stand-in photos (landscape, portrait, square) for the
    real-pictures render, as file:// addresses."""
    from PIL import Image, ImageDraw

    d = out / "_assets"
    d.mkdir(parents=True, exist_ok=True)
    specs = [("a", (1600, 1000), (40, 90, 160), (230, 140, 60)), ("b", (900, 1200), (20, 120, 90), (240, 220, 120)),
             ("c", (1000, 1000), (150, 40, 80), (250, 190, 200)), ("d", (1600, 900), (60, 60, 70), (150, 200, 240))]
    urls = []
    for name, (w, h), c1, c2 in specs:
        f = d / f"{name}.jpg"
        if not f.exists():
            img = Image.new("RGB", (w, h), c1)
            draw = ImageDraw.Draw(img)
            for y in range(h):
                t = y / h
                draw.line([(0, y), (w, y)], fill=tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3)))
            draw.ellipse([w * 0.3, h * 0.25, w * 0.7, h * 0.75], outline=(255, 255, 255), width=max(4, w // 120))
            img.save(f, quality=80)
        urls.append(f.as_uri())
    return urls


def node_bin() -> str:
    return shutil.which("node") or "/opt/homebrew/bin/node"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("keys", nargs="*")
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--no-thumbs", action="store_true")
    ap.add_argument("--no-extras", action="store_true")
    ap.add_argument("--threshold", type=float, default=99.5)
    a = ap.parse_args()

    keys = a.keys or R.packages()
    assets = None if a.no_extras else make_assets(a.out)
    jobs = []
    for key in keys:
        out = a.out / key
        out.mkdir(parents=True, exist_ok=True)
        sample = R.sample(key)
        ours = out / "ours.html"
        ours.write_text(R.render(key, sample), encoding="utf-8")
        extra = []
        if assets:
            movable = [s["key"] for s in R.meta(key)["sections"]]
            for name, data, order in (
                ("images", V.with_images(sample, assets), None),
                ("sparse", V.sparse(key), None),
                ("long", V.long_names(key), None),
                ("reversed", V.with_images(sample, assets), list(reversed(movable))),
            ):
                f = out / f"{name}.html"
                f.write_text(R.render(key, data, order=order), encoding="utf-8")
                extra.append({"name": name, "file": str(f), "mobile": name in ("images", "long")})
        src = source_for(key, a.src)
        jobs.append({"key": key, "original": str(src) if src else None, "rendered": str(ours), "out": str(out),
                     "package": str(R.TEMPLATES_DIR / key), "thumbs": not a.no_thumbs, "extra": extra})
    spec = a.out / "_job.json"
    result = a.out / "_result.json"
    spec.write_text(json.dumps({"jobs": jobs, "result": str(result)}), encoding="utf-8")
    env = dict(os.environ, PATH="/opt/homebrew/bin:" + os.environ.get("PATH", ""))
    subprocess.run([node_bin(), str(HERE / "shoot.cjs"), str(spec)], check=True, env=env)
    res = json.loads(result.read_text(encoding="utf-8"))

    bad = False
    print(f"{'package':<10} {'identical %':>11} {'pixelmatch %':>12} {'height (designer/ours)':>24}  mobile")
    for key in keys:
        r = res.get(key, {})
        p = r.get("parity") or {}
        mob = []
        for name, m in (r.get("mobile") or {}).items():
            over = m["scrollWidth"] > m["viewport"] or bool(m["offenders"])
            bad |= over
            mob.append(f"{name}:{'OVERFLOW' if over else 'ok'}")
        if p and p["identical_pct"] < a.threshold:
            bad = True
        h = f"{p.get('original_height')}/{p.get('rendered_height')}" if p else "-"
        print(f"{key:<10} {p.get('identical_pct', '-'):>11} {p.get('pixelmatch_pct', '-'):>12} {h:>24}  {' '.join(mob)}")
        (a.out / key / "report.json").write_text(json.dumps(r, indent=2), encoding="utf-8")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
