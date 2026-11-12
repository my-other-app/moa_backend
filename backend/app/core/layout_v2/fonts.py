"""The fonts a v2 layout may use: every face in backend/assets/doc-fonts
(the 34 the designs need, plus the v1 editor's five), by CSS family name.

The files, their @font-face rules and their data: URIs are
app.core.doc_designs.fonts' (one place for fonts); this module only says
which faces exist, maps the v1 font keys to their families, and finds the
faces a layout uses so the page carries those and no others.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Iterable, Optional

from app.core.doc_designs import fonts as doc_fonts

#: The v1 editor's font keys and their CSS families (contract, Fonts).
V1_FAMILIES = {
    "inter": "Inter",
    "google_sans": "Google Sans",
    "mono": "Roboto Mono",
    "dm_serif": "DM Serif Display",
    "bebas": "Bebas Neue",
}


@lru_cache(maxsize=1)
def _faces() -> dict[str, tuple[tuple[str, int], ...]]:
    out = {}
    for family, info in doc_fonts.lock()["families"].items():
        out[family] = tuple(sorted((f["style"], int(f["weight"])) for f in info["faces"]))
    return out


def families() -> list[str]:
    return sorted(_faces())


def has_family(family: str) -> bool:
    return family in _faces()


def has_face(family: str, weight: int, style: str = "normal") -> bool:
    return (style, int(weight)) in _faces().get(family, ())


def nearest_face(family: str, weight: int, style: str = "normal") -> Optional[tuple[str, int]]:
    """The face a browser would pick for a weight the family lacks (CSS
    font matching, simplified: the same style if there is one, then the
    closest weight, heavier on a tie above 400)."""
    faces = _faces().get(family)
    if not faces:
        return None
    same = [f for f in faces if f[0] == style] or list(faces)
    return min(same, key=lambda f: (abs(f[1] - weight), -f[1] if weight > 400 else f[1]))


def _distance(a: str, b: str) -> int:
    a, b = a.lower(), b.lower()
    dp = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, len(b) + 1):
            prev, dp[j] = dp[j], min(dp[j] + 1, dp[j - 1] + 1, prev + (a[i - 1] != b[j - 1]))
    return dp[len(b)]


def nearest_names(family: str, n: int = 3) -> list[str]:
    """The families most like a name that is not one (for the message)."""
    names = families()
    key = family.strip().lower()
    starts = [f for f in names if f.lower().startswith(key[:3])] if key else []
    ranked = sorted(names, key=lambda f: (f not in starts, _distance(key, f)))
    return ranked[:n]


def fonts_available() -> list[dict]:
    """Every family a layout may name, with its weights and styles."""
    v1 = {v: k for k, v in V1_FAMILIES.items()}
    out = []
    for family, faces in sorted(_faces().items()):
        entry = {
            "family": family,
            "weights": sorted({w for _, w in faces}),
            "styles": sorted({s for s, _ in faces}),
            "faces": [{"weight": w, "style": s} for s, w in faces],
        }
        if family in v1:
            entry["v1"] = v1[family]
        out.append(entry)
    return out


def faces_used(uses: Iterable[tuple[str, int, str]]) -> tuple[tuple[str, str, int], ...]:
    """(family, style, weight) as doc_designs.fonts takes them, for the faces
    a layout uses, each resolved to one the lock has; unknown families are
    left out (the page then falls back, and problems() has said so)."""
    out: dict[tuple[str, str, int], None] = {}
    for family, weight, style in uses:
        face = nearest_face(family, int(weight), style)
        if face:
            out[(family, face[0], face[1])] = None
    return tuple(sorted(out))
