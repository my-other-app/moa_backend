"""The fonts a design declares, from backend/assets/doc-fonts (contract, D3).

A design's manifest keeps the designer's Google Fonts query; each face it
names (family, style, weight) becomes the @font-face rules Google's own
stylesheet gives a browser for it -- one per script subset, with its
unicode-range -- over the WOFF2 files in the lock. Only the design's own
faces, never every face: a template asking for weight 600 of a family it
loaded only at 500 and 700 gets 700, as it did in the designer's browser.

For the renderer the files go in as data: URIs (`for_render`): the render
service loads nothing from a network or a disk, and only the subsets the
page's characters need are sent (a ticket in English carries the latin
files alone). For a browser preview they are URLs under /static/doc-fonts/
(`for_browser`), loaded by the browser only as needed.

The files are Google's static instances (scripts/doc_fonts.py says why:
the designer's previews were drawn with them). The same faces are installed
in the render image as static TTFs (backend/render/Dockerfile), a family the
stylesheet names resolving there too should a file ever fail to decode.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.parse
from functools import lru_cache
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[3] / "assets" / "doc-fonts"
LOCK = ASSETS / "fonts.lock.json"
#: Where the backend serves ASSETS (app/core/doc_designs/static.py).
STATIC_PREFIX = "/static/doc-fonts"


@lru_cache(maxsize=1)
def lock() -> dict:
    return json.loads(LOCK.read_text())


def digest() -> str:
    """The lock's digest: changes whenever any font file does."""
    return lock()["digest"]


def faces_of(query: str) -> tuple[tuple[str, str, int], ...]:
    """(family, style, weight) for each face a css2 query names."""
    out = []
    for key, value in urllib.parse.parse_qsl(query.lstrip("?")):
        if key != "family":
            continue
        family, _, spec = value.partition(":")
        if not spec:
            out.append((family, "normal", 400))
            continue
        axes, _, tuples = spec.partition("@")
        names = axes.split(",")
        for tup in tuples.split(";"):
            vals = dict(zip(names, tup.split(",")))
            out.append((family, "italic" if vals.get("ital") == "1" else "normal", int(vals.get("wght", 400))))
    return tuple(out)


def _face(family: str, style: str, weight: int) -> dict:
    for face in lock()["families"][family]["faces"]:
        if face["style"] == style and face["weight"] == weight:
            return face
    raise KeyError(f"{family} {style} {weight} is not in fonts.lock.json")


def _ranges(spec: str) -> list[tuple[int, int]]:
    out = []
    for part in spec.split(","):
        lo, _, hi = part.strip()[2:].partition("-")
        out.append((int(lo, 16), int(hi or lo, 16)))
    return out


@lru_cache(maxsize=256)
def _subset_ranges(spec: str) -> tuple[tuple[int, int], ...]:
    return tuple(_ranges(spec))


def _needed(spec: str, codepoints: frozenset[int] | None) -> bool:
    if codepoints is None:
        return True
    ranges = _subset_ranges(spec)
    return any(lo <= cp <= hi for cp in codepoints for lo, hi in ranges)


@lru_cache(maxsize=256)
def _data_uri(file: str) -> str:
    return "data:font/woff2;base64," + base64.b64encode((ASSETS / file).read_bytes()).decode()


def _rules(faces, src, codepoints: frozenset[int] | None) -> str:
    rules = []
    for family, style, weight in faces:
        for w in _face(family, style, weight)["woff2"]:
            if not _needed(w["unicode_range"], codepoints):
                continue
            rules.append(
                "@font-face{font-family:'%s';font-style:%s;font-weight:%d;font-display:swap;"
                "src:url(%s) format('woff2');unicode-range:%s}"
                % (family, style, weight, src(w["file"]), w["unicode_range"])
            )
    return "\n".join(rules)


_ASCII = frozenset(range(0x20, 0x7F))


def codepoints(text: str) -> frozenset[int]:
    """The characters a page uses (its markup's ASCII always among them)."""
    return frozenset(ord(c) for c in set(text)) | _ASCII


def for_render(faces, text: str) -> str:
    """@font-face rules with the files inline, for the subsets `text` needs."""
    return _rules(faces, _data_uri, codepoints(text))


def for_browser(faces, base_url: str) -> str:
    """@font-face rules pointing at the files under /static/doc-fonts/."""
    root = base_url.rstrip("/") + STATIC_PREFIX + "/"
    return _rules(faces, lambda f: root + f, None)


def css_url(base_url: str) -> str:
    """fonts.css (every face), versioned so it may be cached for good."""
    return f"{base_url.rstrip('/')}{STATIC_PREFIX}/fonts.css?v={digest()}"


def served_files() -> frozenset[str]:
    """What /static/doc-fonts/ may answer with: the WOFF2 files, fonts.css,
    the licences and the lock. Not the TTFs (they are the render image's)."""
    names = {"fonts.css", "fonts.lock.json"}
    for info in lock()["families"].values():
        names.add(info["license"]["file"])
        for face in info["faces"]:
            names.update(w["file"] for w in face["woff2"])
    return frozenset(names)


_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def resolve(path: str) -> Path | None:
    """The file a /static/doc-fonts/ path names, if it is one we serve."""
    if not _SAFE.match(path) or ".." in path or path not in served_files():
        return None
    return ASSETS / path
