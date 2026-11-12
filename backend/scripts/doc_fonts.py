"""The fonts the 35 document designs use, self-hosted (doc-designs contract, D3).

    python scripts/doc_fonts.py                 # download again from Google Fonts, rewrite the lock and fonts.css
    python scripts/doc_fonts.py --check         # no network: every file in the lock is there with its sha256
    python scripts/doc_fonts.py --fontconfig    # no network: fc-match finds every TTF by the CSS family name
    python scripts/doc_fonts.py --scan DIR...   # take the requests from <link>s in the HTML files under DIR
    python scripts/doc_fonts.py --add           # download only the faces the lock lacks (keeps every file it has)

What is fetched is exactly what the designer's templates asked Google for:
each design's manifest keeps its original stylesheet query (`google_fonts`,
e.g. ``family=Archivo:wght@500;700;800&family=Inter:wght@400;500;600``).
The queries are merged per family (Archivo from two templates is one
request) and sent to Google's css2 API twice:

- with a User-Agent that cannot read WOFF2 (this script's own), Google
  answers with one static TrueType instance per face: ``ttf/``. These are
  installed system-wide in the render image (backend/render/Dockerfile), so
  a family name always resolves there, even without the stylesheet. Google's
  static families name each weight as a family of its own; the script adds
  the typographic family (name IDs 16/17) so fontconfig finds "Barlow" at
  600 (recorded in the lock as `patched`, with the file's `source_sha256`).
- with the User-Agent of a Chrome that has WOFF2 but not variable fonts
  (Chrome 58), Google answers with the same static instances as WOFF2,
  split by script (latin, latin-ext, ...) with their unicode-ranges:
  ``woff2/``. The renderer embeds these, one @font-face per face and subset,
  exactly as Google's stylesheet declares them
  (app/core/doc_designs/fonts.py), and browsers load them from
  /static/doc-fonts/ for previews, so a preview and a print are the same
  letters.

Why static, not the variable fonts a current Chrome gets: the designer's
previews were drawn with static instances. Fraunces proves it: with its
variable font Chrome sets the optical size from the font size, and the
66 px name on certificate 05 came out 7% narrower than in the preview; the
static instance matches it (99.7% of pixels against 98.8%). Every other
design scores the same or better with the static files. The WOFF2 and TTF
of a face have identical outlines and advances (checked glyph by glyph).

Each family's licence (all SIL OFL 1.1) comes from github.com/google/fonts.

Writes, under backend/assets/doc-fonts/: ttf/, woff2/, licenses/,
fonts.lock.json (family, style, weight, file, sha256, source of every file)
and fonts.css (every face, for browsers). Files no longer in the lock are
deleted. Running it again with nothing changed at Google rewrites the same
bytes; Google does update fonts now and then, so review the diff (and the
parity renders) before committing a refresh.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
OUT = BACKEND / "assets" / "doc-fonts"
MANIFESTS = sorted((BACKEND / "app" / "core" / "doc_designs" / "templates").glob("*/manifest.json"))

CSS2 = "https://fonts.googleapis.com/css2"
#: Google answers this one with static TrueType (no WOFF/WOFF2 support claimed).
UA_TTF = "moa-doc-fonts/1 (TrueType)"
#: And this one with static WOFF2 split by script: Chrome 58 had WOFF2 and
#: unicode-range but not variable fonts.
UA_WOFF2 = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36"
)
LICENSE_URL = "https://raw.githubusercontent.com/google/fonts/main/{dir}/{slug}/{file}"

LOCK = "fonts.lock.json"
CSS = "fonts.css"


# ─── Requests ────────────────────────────────────────────────────────────────


def parse_query(query: str) -> dict[str, list[tuple[int, int, str | None]]]:
    """A css2 query (``family=A:ital,wght@0,400;1,700&family=B&display=swap``)
    as {family: [(ital, wght, opsz range or None), ...]}."""
    out: dict[str, list[tuple[int, int, str | None]]] = defaultdict(list)
    for key, value in urllib.parse.parse_qsl(html.unescape(query).lstrip("?"), keep_blank_values=True):
        if key != "family":
            continue
        family, _, spec = value.partition(":")
        if not spec:
            out[family].append((0, 400, None))
            continue
        axes_part, _, tuples = spec.partition("@")
        axes = axes_part.split(",")
        for tup in tuples.split(";"):
            vals = dict(zip(axes, tup.split(",")))
            out[family].append((int(vals.get("ital", 0)), int(vals.get("wght", 400)), vals.get("opsz")))
    return out


def merged_requests(queries: list[str]) -> dict[str, str]:
    """One css2 family spec per family, covering every face any query asked for."""
    faces: dict[str, set] = defaultdict(set)
    opsz: dict[str, str] = {}
    for query in queries:
        for family, tuples in parse_query(query).items():
            for ital, wght, op in tuples:
                faces[family].add((ital, wght))
                if op:
                    opsz[family] = op
    specs = {}
    for family, fs in sorted(faces.items()):
        fs = sorted(fs)
        name = family.replace(" ", "+")
        italics = any(i for i, _ in fs)
        weights = {w for _, w in fs}
        if family in opsz:
            specs[family] = f"{name}:ital,opsz,wght@" + ";".join(f"{i},{opsz[family]},{w}" for i, w in fs)
        elif italics and weights == {400}:
            specs[family] = f"{name}:ital@" + ";".join(str(i) for i, _ in fs)
        elif italics:
            specs[family] = f"{name}:ital,wght@" + ";".join(f"{i},{w}" for i, w in fs)
        elif weights == {400}:
            specs[family] = name
        else:
            specs[family] = f"{name}:wght@" + ";".join(str(w) for _, w in fs)
    return specs


#: Faces the layout-v2 editor offers besides the designs' own: the v1
#: editor's five fonts, by their CSS names (layout-v2 contract, Fonts).
EDITOR_QUERIES = [
    "family=Inter:wght@400;500;600;700&family=Google+Sans:wght@400;500;600;700"
    "&family=Roboto+Mono:wght@400;700&family=DM+Serif+Display&family=Bebas+Neue",
]


def queries_from_manifests() -> list[str]:
    out = []
    for path in MANIFESTS:
        for design in json.loads(path.read_text())["designs"]:
            out.append(design["google_fonts"])
    return out + EDITOR_QUERIES


def queries_from_html(dirs: list[str]) -> list[str]:
    out = []
    for d in dirs:
        for f in sorted(Path(d).rglob("*.html")):
            for href in re.findall(r'href="https://fonts\.googleapis\.com/css2\?([^"]+)"', f.read_text()):
                out.append(html.unescape(href))
    return out


# ─── Fetching ────────────────────────────────────────────────────────────────


def fetch(url: str, ua: str = UA_TTF) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - fixed https hosts
        return r.read()


def font_faces(css: str) -> list[dict]:
    """The @font-face blocks of a Google stylesheet, with the subset comment
    that precedes each (WOFF2) or None (TTF)."""
    out = []
    for m in re.finditer(r"(?:/\* ([\w-]+) \*/\s*)?@font-face \{(.*?)\}", css, re.S):
        body = m.group(2)
        prop = dict(re.findall(r"([\w-]+):\s*([^;]+);", body))
        out.append({
            "subset": m.group(1),
            "family": prop["font-family"].strip("'\""),
            "style": prop["font-style"],
            "weight": int(prop["font-weight"]),
            "url": re.search(r"url\(([^)]+)\)", prop["src"]).group(1),
            "unicode_range": prop.get("unicode-range"),
        })
    return out


def slug(family: str) -> str:
    return family.lower().replace(" ", "-")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def with_typographic_family(data: bytes, family: str) -> tuple[bytes, str | None]:
    """Google's static families (Barlow, DM Mono, IBM Plex Mono, ...) name
    each weight its own family ("Barlow SemiBold", style "Regular") and have
    no typographic family (name IDs 16 and 17), so fontconfig would not find
    "Barlow" at weight 600. Add them: the CSS name and the style. Returns the
    bytes and what was changed (None when nothing was)."""
    import io

    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(data), recalcTimestamp=False)
    name = font["name"]
    if name.getDebugName(16) or name.getDebugName(1) == family:
        return data, None
    full = name.getDebugName(4) or ""
    style = full[len(family):].strip() if full.startswith(family) else ""
    style = style or "Regular"
    for rec in [r for r in name.names if r.nameID == 1]:
        name.setName(family, 16, rec.platformID, rec.platEncID, rec.langID)
        name.setName(style, 17, rec.platformID, rec.platEncID, rec.langID)
    out = io.BytesIO()
    font.save(out)
    return out.getvalue(), f"name IDs 16/17 added ({family} / {style}) so fontconfig finds it by its CSS family"


def repatch() -> None:
    """Apply with_typographic_family to the TTFs already here (no network)."""
    lock = json.loads((OUT / LOCK).read_text())
    for fam, info in lock["families"].items():
        for face in info["faces"]:
            entry = face["ttf"]
            if "patched" in entry:
                continue
            path = OUT / entry["file"]
            data, patched = with_typographic_family(path.read_bytes(), fam)
            if patched:
                path.write_bytes(data)
                entry.update({"source_sha256": entry["sha256"], "sha256": sha256(data), "patched": patched})
    lock["digest"] = lock_digest(lock["families"])
    (OUT / LOCK).write_text(json.dumps(lock, indent=1, sort_keys=False) + "\n")


def license_for(family: str) -> tuple[bytes, str]:
    name = family.lower().replace(" ", "")
    for directory, file in (("ofl", "OFL.txt"), ("apache", "LICENSE.txt"), ("ufl", "UFL.txt")):
        url = LICENSE_URL.format(dir=directory, slug=name, file=file)
        try:
            return fetch(url), url
        except Exception:  # noqa: BLE001 - try the next licence folder
            continue
    raise SystemExit(f"no licence found for {family}")


def download(requests: dict[str, str]) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="doc-fonts-"))
    for sub in ("ttf", "woff2", "licenses"):
        (tmp / sub).mkdir()
    families = {}
    for family, spec in requests.items():
        print(f"  {spec}", file=sys.stderr)
        url = f"{CSS2}?family={spec}&display=swap"
        ttf_faces = font_faces(fetch(url, UA_TTF).decode())
        woff_faces = font_faces(fetch(url, UA_WOFF2).decode())
        faces = []
        for f in ttf_faces:
            italic = f["style"] == "italic"
            name = f"ttf/{slug(family)}-{f['weight']}{'italic' if italic else ''}.ttf"
            original = fetch(f["url"])
            data, patched = with_typographic_family(original, family)
            (tmp / name).write_bytes(data)
            ttf = {"file": name, "sha256": sha256(data), "source": f["url"]}
            if patched:
                ttf.update({"source_sha256": sha256(original), "patched": patched})
            face = {"style": f["style"], "weight": f["weight"], "ttf": ttf, "woff2": []}
            for w in woff_faces:
                if (w["style"], w["weight"]) != (f["style"], f["weight"]):
                    continue
                data = fetch(w["url"], UA_WOFF2)
                digest = sha256(data)
                wname = f"woff2/{slug(family)}-{w['style']}-{w['subset']}-{digest[:10]}.woff2"
                if not (tmp / wname).exists():
                    (tmp / wname).write_bytes(data)
                face["woff2"].append({
                    "subset": w["subset"], "unicode_range": w["unicode_range"],
                    "file": wname, "sha256": digest, "source": w["url"],
                })
            if not face["woff2"]:
                raise SystemExit(f"Google sent no WOFF2 for {family} {f['style']} {f['weight']}")
            faces.append(face)
        text, lurl = license_for(family)
        lname = f"licenses/{slug(family)}-{Path(urllib.parse.urlparse(lurl).path).name}"
        (tmp / lname).write_bytes(text)
        families[family] = {
            "request": spec,
            "license": {"file": lname, "sha256": sha256(text), "source": lurl},
            "faces": sorted(faces, key=lambda x: (x["style"], x["weight"])),
        }
    return {"tmp": tmp, "families": families}


# ─── Outputs ─────────────────────────────────────────────────────────────────


def lock_digest(families: dict) -> str:
    shas = []
    for fam in sorted(families):
        info = families[fam]
        shas.append(info["license"]["sha256"])
        for face in info["faces"]:
            shas.append(face["ttf"]["sha256"])
            shas.extend(w["sha256"] for w in face["woff2"])
    return sha256("\n".join(shas).encode())[:16]


def face_css(family: str, face: dict, url_for) -> str:
    """Google's own @font-face rules for one face, one per subset."""
    rules = []
    for w in face["woff2"]:
        rules.append(
            "@font-face{font-family:'%s';font-style:%s;font-weight:%d;font-display:swap;"
            "src:url(%s) format('woff2');unicode-range:%s}"
            % (family, face["style"], face["weight"], url_for(w), w["unicode_range"])
        )
    return "\n".join(rules)


def write_css(families: dict, out: Path) -> None:
    head = (
        "/* Generated by backend/scripts/doc_fonts.py from fonts.lock.json -- do not edit.\n"
        "   Every face the document designs use, as Google Fonts serves them to a\n"
        "   browser, from this folder. A design's own HTML declares only its own faces\n"
        "   (app/core/doc_designs/fonts.py). Licences: licenses/ (SIL OFL 1.1). */\n"
    )
    body = "\n".join(
        face_css(fam, face, lambda w: Path(w["file"]).as_posix())
        for fam in sorted(families) for face in families[fam]["faces"]
    )
    (out / CSS).write_text(head + body + "\n")


def regenerate(queries: list[str]) -> None:
    requests = merged_requests(queries)
    print(f"{len(requests)} families", file=sys.stderr)
    got = download(requests)
    tmp, families = got["tmp"], got["families"]
    lock = {
        "comment": "Generated by backend/scripts/doc_fonts.py -- do not edit. Every font file the document "
                   "designs use, where it came from and its sha256.",
        "source": CSS2,
        "user_agents": {"ttf": UA_TTF, "woff2": UA_WOFF2},
        "digest": lock_digest(families),
        "families": families,
    }
    write_css(families, tmp)
    (tmp / LOCK).write_text(json.dumps(lock, indent=1, sort_keys=False) + "\n")
    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(tmp, OUT)
    shutil.rmtree(tmp, ignore_errors=True)
    n_ttf = sum(len(f["faces"]) for f in families.values())
    n_woff = len(list((OUT / "woff2").iterdir()))
    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"{len(families)} families, {n_ttf} TTF faces, {n_woff} WOFF2 files, {size / 1e6:.1f} MB", file=sys.stderr)


def add(queries: list[str]) -> None:
    """Download only the faces the lock does not have yet, and merge them in.
    Every file already in the lock stays byte for byte (a refresh of those is
    `regenerate`, reviewed with the parity renders)."""
    lock = json.loads((OUT / LOCK).read_text())
    families = lock["families"]
    wanted: dict[str, set] = defaultdict(set)
    for query in queries:
        for family, tuples in parse_query(query).items():
            for ital, wght, _op in tuples:
                wanted[family].add((ital, wght))
    missing: dict[str, str] = {}
    for family, faces in sorted(wanted.items()):
        have = {(1 if f["style"] == "italic" else 0, f["weight"]) for f in families.get(family, {}).get("faces", [])}
        need = sorted(faces - have)
        if need:
            name = family.replace(" ", "+")
            missing[family] = merged_requests([f"family={name}:ital,wght@" + ";".join(f"{i},{w}" for i, w in need)])[family]
    if not missing:
        print("nothing to add", file=sys.stderr)
        return
    print(f"adding {len(missing)} families: {', '.join(missing)}", file=sys.stderr)
    got = download(missing)
    tmp = got["tmp"]
    for family, info in got["families"].items():
        for sub in ("ttf", "woff2", "licenses"):
            for f in (tmp / sub).iterdir():
                if not (OUT / sub / f.name).exists():
                    shutil.copy2(f, OUT / sub / f.name)
        if family in families:
            faces = {(f["style"], f["weight"]): f for f in families[family]["faces"]}
            faces.update({(f["style"], f["weight"]): f for f in info["faces"]})
            families[family]["faces"] = sorted(faces.values(), key=lambda x: (x["style"], x["weight"]))
        else:
            families[family] = info
    shutil.rmtree(tmp, ignore_errors=True)
    lock["families"] = dict(sorted(families.items()))
    lock["digest"] = lock_digest(lock["families"])
    write_css(lock["families"], OUT)
    (OUT / LOCK).write_text(json.dumps(lock, indent=1, sort_keys=False) + "\n")


def check() -> int:
    lock = json.loads((OUT / LOCK).read_text())
    bad = []
    listed = {LOCK, CSS}
    for info in lock["families"].values():
        entries = [info["license"]] + [f["ttf"] for f in info["faces"]] + [w for f in info["faces"] for w in f["woff2"]]
        for e in entries:
            listed.add(e["file"])
            p = OUT / e["file"]
            if not p.exists() or sha256(p.read_bytes()) != e["sha256"]:
                bad.append(e["file"])
    extra = [str(p.relative_to(OUT)) for p in OUT.rglob("*") if p.is_file() and str(p.relative_to(OUT)) not in listed]
    if lock_digest(lock["families"]) != lock["digest"]:
        bad.append("digest")
    for b in bad:
        print("mismatch:", b)
    for e in extra:
        print("not in the lock:", e)
    return 1 if bad or extra else 0


#: CSS font-weight -> fontconfig's weight scale (FcWeightFromOpenType).
FC_WEIGHT = {100: 0, 200: 40, 300: 50, 400: 80, 500: 100, 600: 180, 700: 200, 800: 205, 900: 210}


def fontconfig() -> int:
    """Every TTF, alone in a fontconfig of its own folder, is what fc-match
    returns for the CSS family name at its weight and style."""
    if not shutil.which("fc-match"):
        print("fc-match is not installed", file=sys.stderr)
        return 2
    lock = json.loads((OUT / LOCK).read_text())
    with tempfile.TemporaryDirectory() as d:
        conf = Path(d) / "fonts.conf"
        conf.write_text(
            '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>'
            f"<dir>{OUT / 'ttf'}</dir><cachedir>{d}/cache</cachedir></fontconfig>"
        )
        env = {**os.environ, "FONTCONFIG_FILE": str(conf)}
        subprocess.run(["fc-cache", "-f"], env=env, check=True, capture_output=True)
        bad = 0
        for fam, info in lock["families"].items():
            for face in info["faces"]:
                slant = 100 if face["style"] == "italic" else 0
                pattern = f"{fam}:weight={FC_WEIGHT[face['weight']]}:slant={slant}"
                got = subprocess.run(["fc-match", "-f", "%{family}|%{file}", pattern], env=env,
                                     capture_output=True, text=True).stdout
                families, _, file = got.partition("|")
                ok = Path(file).name == Path(face["ttf"]["file"]).name and fam in families.split(",")
                bad += not ok
                print(("ok  " if ok else "BAD ") + f"{pattern:45} -> {Path(file).name} ({families})")
        return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--fontconfig", action="store_true")
    ap.add_argument("--scan", nargs="+", metavar="DIR")
    ap.add_argument("--add", action="store_true")
    ap.add_argument("--repatch", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.repatch:
        repatch()
        return 0
    if args.check:
        return check()
    if args.add:
        add(queries_from_manifests())
        return 0
    if args.fontconfig:
        return fontconfig()
    regenerate(queries_from_html(args.scan) if args.scan else queries_from_manifests())
    return 0


if __name__ == "__main__":
    sys.exit(main())
