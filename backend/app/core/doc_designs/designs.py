"""The 35 designs: one manifest per kind, read once (contract, D4).

`templates/{ticket,certificate,badge}/manifest.json` describes each design:
its page, fonts (the designer's Google Fonts query, now served by us), the
tokens it prints and which may be empty, its accent and the colours that
accent must stand out from, its options (wording, lanyard) and its preview.
`DesignInfo` is that, typed; `to_dict()` is what the editor API returns.

A design's id is "<kind>/<file id>", e.g. "certificate/07-swiss-minimal"
(contract, D6: `config.preset.id`).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Optional

from app.core.doc_designs import fonts
from app.core.doc_designs.compile import Compiled, compile_fragment, compile_template
from app.core.doc_designs.errors import UnknownDesign

ROOT = Path(__file__).resolve().parent / "templates"
#: Where the backend serves the designs' preview images (static.py).
PREVIEW_PREFIX = "/static/doc-designs"
KINDS = ("ticket", "certificate", "badge")


@dataclass(frozen=True)
class PageSpec:
    width_px: int
    height_px: int
    width_mm: float
    height_mm: float

    @property
    def paper(self) -> tuple[str, str]:
        """Gotenberg's paperWidth and paperHeight."""
        return (f"{self.width_mm:g}mm", f"{self.height_mm:g}mm")

    def to_dict(self) -> dict:
        return {"width_px": self.width_px, "height_px": self.height_px, "width_mm": self.width_mm, "height_mm": self.height_mm}


@dataclass(frozen=True)
class AccentInfo:
    """`mode` "token": the template says {{accent_color}} (certificates);
    "var": it reads the CSS variable --accent (ID cards)."""

    mode: str
    default: str
    suggestions: tuple[str, ...]
    against: tuple[str, ...]
    min_contrast: float
    note: str

    def to_dict(self) -> dict:
        return {"default": self.default, "suggestions": list(self.suggestions), "against": list(self.against),
                "min_contrast": self.min_contrast, "note": self.note}


@dataclass(frozen=True)
class DesignInfo:
    id: str
    kind: str
    key: str
    name: str
    use: str
    has_photo: bool
    colour: str  # "colour" | "mono" (prints on a black-only printer)
    page: PageSpec
    accent: Optional[AccentInfo]
    tokens: Mapping[str, str]  # token -> "required" | "optional"
    options: Mapping[str, Any]  # option name -> its schema, with "default"
    fonts: tuple[tuple[str, str, int], ...]
    file: Path
    preview: Optional[Path]
    version: str  # the kind's manifest version
    digest: str  # the template file and its manifest entry, hashed
    qr: Mapping[str, Any]
    raw: Mapping[str, Any] = field(repr=False, compare=False, default_factory=dict)

    @property
    def optional_tokens(self) -> list[str]:
        """What a club may bind: auto, fixed text, a registration question, or hidden."""
        return sorted(t for t, need in self.tokens.items() if need == "optional")

    def option_defaults(self) -> dict:
        return {name: spec.get("default") for name, spec in self.options.items() if "default" in spec}

    def preview_url(self, base_url: Optional[str] = None) -> Optional[str]:
        """The preview image, for a browser (app/core/doc_designs/static.py)."""
        if not self.preview:
            return None
        if base_url is None:
            from app.config import settings

            base_url = settings.PUBLIC_API_URL
        return f"{base_url.rstrip('/')}{PREVIEW_PREFIX}/{self.kind}/{self.key}.png?v={self.digest}"

    def to_dict(self, base_url: Optional[str] = None) -> dict:
        """What the editor reads: no file paths, no HTML."""
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "use": self.use,
            "has_photo": self.has_photo,
            "colour": self.colour,
            "page": self.page.to_dict(),
            "accent": self.accent.to_dict() if self.accent else None,
            "tokens": dict(self.tokens),
            "bindable": self.optional_tokens,
            "options": {k: dict(v) for k, v in self.options.items()},
            "preview": self.preview_url(base_url),
            "version": self.digest,
        }


def _accent(kind_manifest: dict, d: dict) -> Optional[AccentInfo]:
    a = d.get("accent")
    if not a:
        return None
    return AccentInfo(
        mode=kind_manifest["accent_mode"],
        default=a["default"].upper(),
        suggestions=tuple(s.upper() for s in a.get("suggestions", [])),
        against=tuple(c.upper() for c in a.get("against", [])),
        min_contrast=float(a.get("min_contrast", 3.0)),
        note=a.get("note", ""),
    )


def _options(kind_manifest: dict, d: dict) -> dict:
    out = {k: dict(v) for k, v in kind_manifest.get("options", {}).items()}
    if "wordings" in d:
        out["wording"] = {
            "type": "choice",
            "choices": list(d["wordings"]),
            "default": d["default_wording"],
            "label": "Wording",
        }
        out["text"] = {
            "type": "text",
            "slots": sorted({slot for w in d["wordings"].values() for slot in (w or {})} | set(d.get("slots", []))),
            "max_length": 200,
            "label": "Your own words for a line",
        }
    return out


@lru_cache(maxsize=1)
def _load() -> dict[str, DesignInfo]:
    out: dict[str, DesignInfo] = {}
    for kind in KINDS:
        path = ROOT / kind / "manifest.json"
        m = json.loads(path.read_text())
        assert m["kind"] == kind, path
        page = PageSpec(**m["page"])
        for d in m["designs"]:
            file = ROOT / kind / d["file"]
            raw_entry = json.dumps(d, sort_keys=True).encode()
            digest = hashlib.sha256(file.read_bytes() + b"\0" + raw_entry + b"\0" + m["version"].encode()).hexdigest()[:16]
            preview = ROOT / kind / d["preview"] if d.get("preview") else None
            info = DesignInfo(
                id=f"{kind}/{d['key']}",
                kind=kind,
                key=d["key"],
                name=d["name"],
                use=d.get("use", ""),
                has_photo=bool(d.get("has_photo")),
                colour=d.get("colour", "colour"),
                page=page,
                accent=_accent(m, d),
                tokens=dict(d["tokens"]),
                options=_options(m, d),
                fonts=fonts.faces_of(d["google_fonts"]),
                file=file,
                preview=preview,
                version=m["version"],
                digest=digest,
                qr=m["qr"],
                raw=d,
            )
            out[info.id] = info
    return out


def catalogue(kind: str) -> list[DesignInfo]:
    """Every design of a kind, in the designer's order."""
    if kind not in KINDS:
        raise UnknownDesign(f"no such kind: {kind}")
    return [d for d in _load().values() if d.kind == kind]


def get(design_id: str) -> DesignInfo:
    try:
        return _load()[design_id]
    except KeyError:
        raise UnknownDesign(f"no such design: {design_id}") from None


@lru_cache(maxsize=64)
def compiled(design_id: str) -> Compiled:
    return compile_template(get(design_id).file.read_text())


@lru_cache(maxsize=256)
def wording(design_id: str, wording_key: str, slot: str) -> Optional[list]:
    """The nodes of one slot's wording, or None: the template's own words."""
    d = get(design_id)
    html = (d.raw.get("wordings", {}).get(wording_key) or {}).get(slot)
    return compile_fragment(html) if html is not None else None
